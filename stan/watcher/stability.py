"""File stability detection for Bruker .d directories and Thermo .raw files."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StabilityTracker:
    """Track whether a raw data file/directory has finished writing.

    Bruker .d: directory whose total size is checked every 10s.
    Thermo .raw: single binary file whose size is checked every 10s.
    Stable when size is unchanged for ``stable_secs`` consecutive seconds.
    """

    path: Path
    vendor: str  # "bruker" | "thermo"
    stable_secs: int = 60
    _size_history: list[tuple[float, int]] = field(default_factory=list)
    _last_check: float = 0.0

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

        self._size_history.append((now, size))

        # Prune entries older than stable_secs
        cutoff = now - self.stable_secs
        self._size_history = [(t, s) for t, s in self._size_history if t >= cutoff]

        min_checks = self.stable_secs // 10
        if len(self._size_history) < min_checks:
            return False

        sizes = [s for _, s in self._size_history]
        return len(set(sizes)) == 1 and sizes[0] > 0
