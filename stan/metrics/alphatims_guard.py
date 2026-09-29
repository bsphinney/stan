"""Refuse alphatims frame windows that pandas 3 has silently shifted.

alphatims 1.0.8 prepends a dummy frame 0 to its frame table and zeroes it
with chained assignment (``frames.Id[0] = 0``, ``frames.NumPeaks[0] = 0``,
``frames.MsMsType[0] = 0``). Under pandas 3 Copy-on-Write is always on, so
those assignments land on a temporary copy and do nothing: the dummy keeps
frame 1's values, and every event window alphatims derives from the table
starts NumPeaks(frame 1) events early. ``TimsTOF[fid]`` for an MS1 frame
then returns the tail of the preceding diaPASEF MS2 frames plus most of the
MS1 frame, and drops the MS1 frame's own last events.

The Hive venv ran pandas 3.0.2 from 2026-05-07 19:27 PDT until the fix in
v1.2.2. On the timsTOF HT that biased stored PEG share by +15-26 %
relative (a heavy HeLa QC: 19.51 % stored, 16.38 % with the true frame,
measured event-for-event against analysis.tdf_bin, jobs 24190826 and
24191566), and window drift read the same shifted windows.

The check is on behaviour, not versions: whatever pandas or alphatims is
installed, the dummy row must be zeroed or the windows are wrong.
"""

from __future__ import annotations

from typing import Any


def frame_table_problem(data: Any) -> str | None:
    """Explain why an alphatims TimsTOF's frame windows can't be trusted.

    Args:
        data: an ``alphatims.bruker.TimsTOF`` (anything with a ``frames``
            DataFrame carrying ``Id`` and ``NumPeaks`` columns).

    Returns:
        None when frame 0 is the zeroed dummy alphatims intends, else a
        one-line reason suitable for a log line or an "unavailable" status.
    """
    try:
        frames = data.frames
        dummy_id = int(frames["Id"].iloc[0])
        dummy_peaks = int(frames["NumPeaks"].iloc[0])
    except Exception as e:  # noqa: BLE001 - any failure means "can't vouch for it"
        return f"cannot check alphatims' frame table ({type(e).__name__}: {e})"
    if dummy_id != 0 or dummy_peaks != 0:
        import pandas

        return (
            f"alphatims' dummy frame 0 was not zeroed (Id={dummy_id}, "
            f"NumPeaks={dummy_peaks}); with pandas {pandas.__version__} every "
            "frame window is shifted. Install pandas<3 (stan-proteomics[peg])."
        )
    return None
