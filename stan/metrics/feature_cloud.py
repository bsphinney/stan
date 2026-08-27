"""Charge-labeled ion-cloud extraction from 4DFF ``.features`` sidecars.

Why this module exists
----------------------
The dashboard's "Ion cloud" tab prefers a Plotly scatter with one trace
per charge state (see CLAUDE.md, *Dashboard: Ion Cloud View*). Until
v1.0.13 the API served that view by opening the ``.features`` SQLite
file sitting next to the Bruker ``.d`` — which only works when the
dashboard process can see the raw data on its own filesystem.

That assumption is false for the fleet deployment:

* raw data lives on Hive / the Flinders NFS export
  (``/nfs/lssc0/flinders/...``),
* the canonical store is PG Farm,
* ``stan dashboard`` runs on Brett's Mac (or an instrument PC) and
  reads a local SQLite that ``stan.sync.pg_to_sqlite`` refreshes from
  PG every 5 minutes.

So every run showed "no .features file found next to raw data" even
though 4DFF had written a perfectly good sidecar on Hive hours earlier.

The fix is to decouple the view from the filesystem: extract a
downsampled, charge-labeled point cloud *where the file lives* (Hive),
store it centrally, and let the dashboard read it like any other
metric. This module is the extraction half.

Storage is a separate table (``feature_clouds``) rather than extra
columns on ``drift_peak_clouds`` on purpose: the two clouds have
different provenance (4DFF deconvolved features vs. raw MS1 peaks from
``detect_window_drift``) and different writers, and sharing a primary
key would mean whichever backfill ran last silently clobbered the
other.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Points kept per run. Matches the house convention set by
# ``stan.db.insert_drift_peak_cloud`` — keep the two clouds on the same
# budget so a run's storage cost doesn't depend on which view produced
# it. 4DFF emits ~25-45 k features for a 30-100 SPD HeLa gradient, so
# this is typically a 5-9x downsample; a run's dominant +2 ridge still
# lands ~3,500 points, and the +1 contamination band ~270, which is
# enough to read both.
#
# Measured cost at this cap: ~70 KB per run on disk in PG (the JSON
# TOAST-compresses roughly 3x), so a fully backfilled 1,639-run fleet is
# ~110 MB. Raising it is a one-flag change (`--max-points`), but raise it
# deliberately: it is the single biggest lever on this table's size.
DEFAULT_MAX_POINTS = 5_000


@dataclass
class FeatureCloud:
    """A downsampled, charge-labeled MS1 feature cloud for one run."""

    mz: list[float] = field(default_factory=list)
    mobility: list[float] = field(default_factory=list)
    rt: list[float] = field(default_factory=list)
    charge: list[int] = field(default_factory=list)
    intensity: list[float] = field(default_factory=list)
    n_total: int = 0
    features_path: str = ""

    @property
    def n_points(self) -> int:
        return len(self.mz)

    def by_charge(self) -> dict[str, dict[str, list]]:
        """Bucket the points per charge state.

        Returns the exact shape the dashboard's ``DriftCloudPlotly``
        component expects: ``{"2": {"mz": [...], "mobility": [...],
        "rt": [...], "intensity": [...]}, ...}``.
        """
        out: dict[str, dict[str, list]] = {}
        for i, z in enumerate(self.charge):
            b = out.setdefault(
                str(int(z)),
                {"mz": [], "mobility": [], "rt": [], "intensity": []},
            )
            b["mz"].append(self.mz[i])
            b["mobility"].append(self.mobility[i])
            b["rt"].append(self.rt[i])
            b["intensity"].append(self.intensity[i])
        return out


def extract_feature_cloud(
    features_path: str | Path,
    max_points: int = DEFAULT_MAX_POINTS,
) -> FeatureCloud:
    """Read ``LcTimsMsFeature`` and return a downsampled cloud.

    Uses a plain ``sqlite3`` connection rather than
    ``stan.metrics.features.read_features`` so this stays importable on
    hosts without polars (the Hive backfill venv is deliberately thin).

    Downsampling is a uniform ``rowid % step`` stride, not a random
    sample: it is deterministic (re-running the backfill produces the
    same picture), it costs nothing on the SQLite side, and because 4DFF
    writes features in acquisition order it preserves the relative
    density of every region of the cloud — which is the whole point of
    the view.

    Args:
        features_path: The ``<name>.d.features`` SQLite file.
        max_points: Upper bound on points kept.

    Returns:
        A :class:`FeatureCloud`. Empty (``n_points == 0``) when the file
        has no usable rows.

    Raises:
        FileNotFoundError: The file does not exist.
        RuntimeError: ``LcTimsMsFeature`` is missing (4DFF aborted).
    """
    import sqlite3

    features_path = Path(features_path)
    if not features_path.exists():
        raise FileNotFoundError(f"Features file not found: {features_path}")

    con = sqlite3.connect(f"file:{features_path}?mode=ro", uri=True)
    try:
        have = {
            r[1]
            for r in con.execute("PRAGMA table_info(LcTimsMsFeature)").fetchall()
        }
        if not have:
            raise RuntimeError(
                f"LcTimsMsFeature table missing from {features_path}. "
                "4DFF may have aborted mid-run."
            )
        total = con.execute(
            "SELECT COUNT(*) FROM LcTimsMsFeature WHERE Intensity > 0"
        ).fetchone()[0]

        # Ceil-style division so 60_000 / 50_000 gives step=2, not 1 —
        # a floor here would leave the result above the cap.
        if total > max_points:
            step = max(2, (total + max_points - 1) // max_points)
            cursor = con.execute(
                "SELECT MZ, Charge, RT, Mobility, Intensity FROM LcTimsMsFeature "
                "WHERE Intensity > 0 AND (rowid % ?) = 0",
                (step,),
            )
        else:
            cursor = con.execute(
                "SELECT MZ, Charge, RT, Mobility, Intensity FROM LcTimsMsFeature "
                "WHERE Intensity > 0"
            )

        cloud = FeatureCloud(n_total=int(total), features_path=str(features_path))
        for mz, z, rt, im, inten in cursor:
            mz = float(mz or 0.0)
            im = float(im or 0.0)
            if mz <= 0 or im <= 0:
                continue
            cloud.mz.append(round(mz, 4))
            cloud.mobility.append(round(im, 5))
            cloud.rt.append(round(float(rt or 0.0), 2))
            cloud.charge.append(int(z or 0))
            cloud.intensity.append(round(float(inten or 0.0), 1))
    finally:
        con.close()

    logger.debug(
        "feature cloud: %d/%d points from %s",
        cloud.n_points, cloud.n_total, features_path.name,
    )
    return cloud


def cloud_to_json(cloud: FeatureCloud, run_id: str, run_name: str = "",
                  source: str = "runs") -> dict:
    """Serialise a cloud to the on-disk cache format.

    The cache exists because extraction has to happen where the raw data
    lives (Hive) while the write target may be unreachable from there —
    PG's ``feature_clouds`` needs an owner-run migration, and the service
    account cannot create it. Dropping ``<run_id>.json`` on the shared
    quobyte export lets whichever host *can* write the DB pick it up
    later with ``stan backfill-feature-cloud --from-cache``.
    """
    from datetime import datetime, timezone

    return {
        "run_id": run_id,
        "run_name": run_name,
        "source": source,
        "mz": cloud.mz,
        "mobility": cloud.mobility,
        "rt": cloud.rt,
        "charge": [int(z) for z in cloud.charge],
        "intensity": cloud.intensity,
        "n_points": cloud.n_points,
        "n_total": cloud.n_total,
        "features_path": cloud.features_path,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def load_feature_cloud_json(path: str | Path) -> FeatureCloud:
    """Read a cached cloud written by :func:`cloud_to_json`.

    Raises ``ValueError`` when the arrays don't line up, so a truncated
    or half-written cache file fails loudly at load rather than storing a
    cloud whose charges belong to different points than its m/z values.
    """
    import json

    data = json.loads(Path(path).read_text())
    cloud = FeatureCloud(
        mz=list(data.get("mz") or []),
        mobility=list(data.get("mobility") or []),
        rt=list(data.get("rt") or []),
        charge=[int(z) for z in (data.get("charge") or [])],
        intensity=list(data.get("intensity") or []),
        n_total=int(data.get("n_total") or 0),
        features_path=str(data.get("features_path") or ""),
    )
    n = len(cloud.mz)
    if not (n == len(cloud.mobility) == len(cloud.rt)
            == len(cloud.charge) == len(cloud.intensity)):
        raise ValueError(f"ragged cloud arrays in {path}")
    if not cloud.n_total:
        cloud.n_total = n
    return cloud


def publish_feature_cloud(
    raw_path: str | Path,
    run_id: str,
    *,
    table: str = "runs",
    db_path=None,
    max_points: int = DEFAULT_MAX_POINTS,
) -> int:
    """Extract a run's cloud from its sidecar and store it. Best-effort.

    Called right after 4DFF finishes, so a run is ion-cloud-viewable the
    moment it lands rather than only after someone remembers to run
    ``stan backfill-feature-cloud``. Returns the number of points stored,
    0 when there is nothing to do (Thermo run, no sidecar, empty table)
    and on any failure — this sits in the ingest path and must never be
    able to fail a run.
    """
    try:
        from stan.db import insert_feature_cloud
        from stan.metrics.features import find_features_file

        d = Path(raw_path)
        if not (d.is_dir() and d.suffix.lower() == ".d"):
            return 0
        feat = find_features_file(d)
        if feat is None:
            return 0
        cloud = extract_feature_cloud(feat, max_points=max_points)
        if cloud.n_points == 0:
            return 0
        return insert_feature_cloud(
            run_id=run_id, mz=cloud.mz, mobility=cloud.mobility,
            rt=cloud.rt, charge=cloud.charge, intensity=cloud.intensity,
            n_total=cloud.n_total, features_path=str(feat),
            table=table, db_path=db_path,
        )
    except Exception:
        logger.warning(
            "ion-cloud publish failed for %s", Path(raw_path).name, exc_info=True
        )
        return 0
