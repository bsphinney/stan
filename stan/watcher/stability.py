"""File stability detection for Bruker .d directories and Thermo .raw files."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path


# Bruker .d in "just-created, method-files-only" state is typically
# under 1 MB. Any real acquisition produces tens of MB to GB via
# analysis.tdf_bin. Use 5 MB as the threshold below which we refuse
# to declare stability, so we don't trigger the monitor/search pipeline
# during the first 60 s when only method files have been written.
_BRUKER_MIN_STABLE_SIZE = 5 * 1024 * 1024


@dataclass
class StabilityTracker:
    """Track whether a raw data file/directory has finished writing.

    Bruker .d: directory whose total size is checked every 10s.
    Thermo .raw: single binary file whose size is checked every 10s.
    Stable when size is unchanged for ``stable_secs`` consecutive seconds.

    For Bruker specifically, we also require evidence that actual
    acquisition data was written — a .d that only contains method
    files (~100-500 KB) looks "stable" during its first minute but
    hasn't started recording yet. v0.2.100 adds a min-size threshold
    + "must have seen growth" guard to prevent the pipeline firing
    on empty .d directories.
    """

    path: Path
    vendor: str  # "bruker" | "thermo"
    stable_secs: int = 60
    _size_history: list[tuple[float, int]] = field(default_factory=list)
    _last_check: float = 0.0
    last_size: int | None = None
    _saw_growth: bool = False
    _initial_size: int | None = None

    def check(self) -> bool:
        """Return True if file/directory is stable (acquisition complete)."""
        now = time.time()
        if now - self._last_check < 10:
            return False
        self._last_check = now

        if self.vendor == "bruker":
            # .d is a directory — sum size of all files inside
            try:
                size = sum(
                    f.stat().st_size for f in self.path.rglob("*") if f.is_file()
                )
            except OSError:
                return False
        else:
            # Thermo .raw is a single file
            try:
                size = self.path.stat().st_size
            except OSError:
                return False

        # Track the smallest-seen size as "initial", and flag as soon as
        # we observe growth past it — this distinguishes a real
        # acquisition from a just-created .d that hasn't started yet.
        if self._initial_size is None:
            self._initial_size = size
        elif size > self._initial_size:
            self._saw_growth = True

        self.last_size = size
        self._size_history.append((now, size))

        # Prune entries older than stable_secs
        cutoff = now - self.stable_secs
        self._size_history = [(t, s) for t, s in self._size_history if t >= cutoff]

        min_checks = self.stable_secs // 10
        if len(self._size_history) < min_checks:
            return False

        sizes = [s for _, s in self._size_history]
        size_stable = (len(set(sizes)) == 1 and sizes[0] > 0)
        if not size_stable:
            return False

        # Vendor-specific sanity checks to avoid firing on a .d that's
        # never actually received acquisition data.
        if self.vendor == "bruker":
            if sizes[0] < _BRUKER_MIN_STABLE_SIZE:
                return False
            # Belt-and-braces: require analysis.tdf_bin to exist and
            # have size (real data present), in case MIN_STABLE_SIZE
            # is exceeded by something unexpected.
            tdf_bin = self.path / "analysis.tdf_bin"
            if not tdf_bin.exists() or tdf_bin.stat().st_size == 0:
                return False
            # If we never saw the size grow past its initial value,
            # we caught a pre-populated .d (e.g., mid-copy from
            # somewhere else) — still wait to be safe.
            if not self._saw_growth:
                return False

        return True
