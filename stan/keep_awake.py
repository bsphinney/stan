"""Windows-only keep-awake helper. Prevents screen saver, monitor sleep,
and idle hibernate while stan watch is running. Implemented via
SetThreadExecutionState — the documented Windows API used by media
players, presentation apps, etc.

Limitations:
- Does not prevent user-initiated lock (Win+L)
- Does not prevent Group Policy auto-lock-after-inactivity
- Does not unlock an already-locked desktop

For instrument PCs, this is enough to keep the screen capturable as
long as the operator hasn't pressed Win+L. See
docs/screencap_setup.md (godmode repo) for the full picture.
"""

from __future__ import annotations

import logging
import platform

logger = logging.getLogger(__name__)

# Windows ES_* flags (kernel32 SetThreadExecutionState).
ES_CONTINUOUS        = 0x80000000
ES_DISPLAY_REQUIRED  = 0x00000002
ES_SYSTEM_REQUIRED   = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040  # not used, documenting only


def keep_awake() -> bool:
    """Tell Windows: don't sleep, don't dim, don't screensaver.

    Returns True if the call succeeded (Windows + ctypes available),
    False on non-Windows or if the API call fails. Idempotent —
    safe to call repeatedly.

    Effective until the calling thread exits or release_awake() is
    called.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        flags = ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED
        result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        if result == 0:
            logger.warning("SetThreadExecutionState returned 0 — keep-awake not active")
            return False
        logger.info("Windows keep-awake enabled (no sleep / no dim / no screensaver)")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Windows keep-awake unavailable: %s", exc)
        return False


def release_awake() -> None:
    """Restore default sleep/dim/screensaver behaviour. Call at clean shutdown."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        logger.debug("Windows keep-awake released")
    except Exception:  # noqa: BLE001
        pass
