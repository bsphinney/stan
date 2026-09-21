"""The one safe way to open a Bruker ``analysis.tdf``.

A Bruker ``.d`` holds its frame index in ``analysis.tdf`` — a plain SQLite
database — beside the spectra in ``analysis.tdf_bin``. A copy taken off the
instrument can carry a **stale, mid-acquisition ``analysis.tdf-wal``** next to
an otherwise finished ``analysis.tdf``. That makes how we open the file a data
integrity question, not a style question:

* A **read-write** open (``sqlite3.connect(str(tdf))``) makes SQLite checkpoint
  that stale WAL into the finished database and **truncate it to the
  mid-acquisition size**. The frame index is destroyed permanently. The spectra
  in ``analysis.tdf_bin`` survive but are no longer addressable, so the run is
  unrecoverable. 350 ``.d`` on the cluster have already been damaged this way —
  the largest single cluster was 63 files in 77 minutes on 2026-04-27.
* A plain **``mode=ro``** open does not truncate, but it reads *through* the
  stale WAL, so metrics are silently computed from mid-acquisition state, and
  it drops an ``analysis.tdf-shm`` inside the raw ``.d``.
* **``?mode=ro&immutable=1``** is the only correct open: SQLite reads the bytes
  as they lie on disk, takes no locks and writes nothing — not the database,
  not a WAL, not an shm. That is exactly right for a tdf, because nothing
  legitimately writes one after acquisition has finished.

So: never call ``sqlite3.connect`` on a tdf directly. Call :func:`connect_tdf`,
or build the URI with :func:`tdf_read_uri`. ``tests/test_tdf_immutable_guard.py``
walks the repo and fails the build on any open that does not.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

__all__ = ["tdf_read_uri", "connect_tdf", "synthetic_tdf_write_uri"]


def _file_uri(path: str | os.PathLike[str]) -> str:
    """Absolute ``file:`` URI for ``path``, with URI metacharacters escaped.

    ``Path.as_uri()`` percent-encodes ``?``, ``#`` and ``%``. An f-string does
    not, which is why hand-built URIs are banned here: a ``.d`` named
    ``Sample#3 50%.d`` turns into a URI where SQLite reads the ``#`` as the
    start of a fragment and opens the wrong file — or, worse, silently opens a
    *new empty* database and the caller sees a tdf with no Frames table.
    """
    return Path(os.path.abspath(os.fspath(path))).as_uri()


def tdf_read_uri(path: str | os.PathLike[str]) -> str:
    """SQLite URI that reads a Bruker ``analysis.tdf`` without writing anything.

    Args:
        path: Path to the ``analysis.tdf`` file (not the ``.d`` directory).

    Returns:
        A ``file:...?mode=ro&immutable=1`` URI, safe to pass to
        ``sqlite3.connect(..., uri=True)``.
    """
    return _file_uri(path) + "?mode=ro&immutable=1"


def connect_tdf(
    path: str | os.PathLike[str],
    *,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    """Open a Bruker ``analysis.tdf`` read-only and immutable.

    This is the only sanctioned way to read a tdf. It never writes to the
    ``.d`` — no checkpoint, no WAL, no shm — so it is safe to run against raw
    data on a shared filesystem, including a ``.d`` carrying a stale WAL from
    an interrupted acquisition.

    Args:
        path: Path to the ``analysis.tdf`` file.
        timeout: Passed through for call-site compatibility. An immutable open
            takes no locks, so this never actually comes into play.

    Returns:
        An open read-only connection.

    Raises:
        sqlite3.Error: If the file is missing or is not a SQLite database.
    """
    return sqlite3.connect(tdf_read_uri(path), uri=True, timeout=timeout)


def synthetic_tdf_write_uri(path: str | os.PathLike[str]) -> str:
    """Writable URI for building a **synthetic** tdf in a test fixture.

    Fixtures that fabricate a tdf have to write one, so they genuinely need a
    read-write handle. Routing them through this named constructor is how they
    declare that intent at the call site: the repo guard recognises the name
    and lets that one open through. That keeps the guard honest — it exempts a
    specific deliberate call, not a whole file, so a real tdf open sneaking
    into an exempted test file is still caught.

    Never call this on a real ``.d``. It is read-write, which is the exact
    operation that destroys an acquisition's frame index.

    Args:
        path: Path to the synthetic ``analysis.tdf`` to create, under a
            temporary directory.

    Returns:
        A plain ``file:`` URI with no mode parameter (read-write, creating).
    """
    return _file_uri(path)
