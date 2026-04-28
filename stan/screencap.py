"""Screen capture daemon for STAN — heartbeat + acquisition-end snapshots.

Captures the operator's screen on a schedule and when a raw file becomes
stable, so analysts can see what was on screen during a run.

Privacy note: Use mask_regions to black out personal info before enabling.
Validate with ``stan screencap-preview`` before turning on the daemon.
"""

from __future__ import annotations

import logging
import os
import platform
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Marker embedded in run-end filenames so prune logic can exempt them.
_RUNEND_MARKER = "_runend_"


@dataclass(frozen=True)
class ScreencapConfig:
    """Configuration for the screen capture daemon."""

    enabled: bool = False
    heartbeat_min: int = 15
    on_acquisition_end: bool = True
    window_titles: list[str] = field(default_factory=list)
    fallback_full_screen: bool = True
    capture_all_windows: bool = False  # capture every match in window_titles, not just the first
    mask_regions: list[dict] = field(default_factory=list)  # [{x, y, w, h}, ...]
    quality: int = 80  # JPEG quality 1-95
    max_dimension: int = 1280  # downsize so longest side <= this
    local_dir: Path = field(default_factory=lambda: Path.home() / "STAN" / "screencaps")
    mirror_dir: Path | None = None  # None = no mirror sync
    local_retention_days: int = 7
    mirror_retention_hours: int = 48


def load_screencap_config(path: Path | None = None) -> ScreencapConfig:
    """Load ~/.stan/screencap.yml; return defaults if missing.

    Args:
        path: Override config file path. Defaults to ~/.stan/screencap.yml
              (Unix) or ~/STAN/screencap.yml (Windows).

    Returns:
        ScreencapConfig populated from file, or all-defaults if file missing.
    """
    if path is None:
        if platform.system() == "Windows":
            config_dir = Path.home() / "STAN"
        else:
            config_dir = Path.home() / ".stan"
        path = config_dir / "screencap.yml"

    if not path.exists():
        logger.debug("screencap.yml not found at %s — using defaults", path)
        return ScreencapConfig()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Failed to parse %s: %s — using defaults", path, exc)
        return ScreencapConfig()

    # Build kwargs, only setting recognised keys.
    kwargs: dict[str, Any] = {}

    bool_keys = ("enabled", "on_acquisition_end", "fallback_full_screen",
                 "capture_all_windows")
    int_keys = ("heartbeat_min", "quality", "max_dimension",
                "local_retention_days", "mirror_retention_hours")

    for k in bool_keys:
        if k in raw:
            kwargs[k] = bool(raw[k])
    for k in int_keys:
        if k in raw:
            kwargs[k] = int(raw[k])

    if "window_titles" in raw:
        wt = raw["window_titles"]
        kwargs["window_titles"] = list(wt) if wt else []
    if "mask_regions" in raw:
        mr = raw["mask_regions"]
        kwargs["mask_regions"] = list(mr) if mr else []
    if "local_dir" in raw:
        kwargs["local_dir"] = Path(raw["local_dir"])
    if "mirror_dir" in raw and raw["mirror_dir"]:
        kwargs["mirror_dir"] = Path(raw["mirror_dir"])

    return ScreencapConfig(**kwargs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _grab_window(title: str) -> "Any | None":
    """Try to grab the window region matching title.

    Returns an mss screenshot object, or None if the window is not found
    or the platform doesn't support window-by-title lookup.
    """
    try:
        import pygetwindow as gw  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("pygetwindow not available — skipping window-by-title capture")
        return None

    # macOS pygetwindow has a different / limited API.
    try:
        windows = gw.getWindowsWithTitle(title)
    except Exception as exc:
        logger.debug("pygetwindow.getWindowsWithTitle(%r) failed: %s", title, exc)
        return None

    if not windows:
        return None

    win = windows[0]

    # Some platforms expose left/top/width/height; gracefully skip if absent.
    try:
        region = {
            "left": int(win.left),
            "top": int(win.top),
            "width": int(win.width),
            "height": int(win.height),
        }
        if region["width"] <= 0 or region["height"] <= 0:
            return None
    except AttributeError:
        logger.debug("Window object lacks geometry attributes — falling back")
        return None

    try:
        import mss  # type: ignore[import-untyped]
        with mss.mss() as sct:
            shot = sct.grab(region)
        return shot
    except Exception as exc:
        logger.debug("mss window grab failed: %s", exc)
        return None


def _grab_fullscreen() -> "Any | None":
    """Grab the primary monitor. Returns an mss screenshot or None."""
    try:
        import mss  # type: ignore[import-untyped]
        with mss.mss() as sct:
            # monitors[0] is the virtual all-monitors bounding box;
            # monitors[1] is the primary physical monitor.
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            return sct.grab(monitor)
    except Exception as exc:
        logger.debug("mss full-screen grab failed: %s", exc)
        return None


def _mss_to_pil(shot: "Any") -> "Any":
    """Convert an mss screenshot object to a PIL Image (RGBA → RGB)."""
    from PIL import Image  # type: ignore[import-untyped]
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img


def _apply_masks(image: "Any", mask_regions: list[dict]) -> "Any":
    """Draw solid black rectangles over mask_regions in place.

    Args:
        image: PIL Image.
        mask_regions: list of dicts with keys x, y, w, h.

    Returns:
        Modified PIL Image (same object).
    """
    if not mask_regions:
        return image

    from PIL import ImageDraw  # type: ignore[import-untyped]
    draw = ImageDraw.Draw(image)
    for region in mask_regions:
        try:
            x = int(region["x"])
            y = int(region["y"])
            w = int(region["w"])
            h = int(region["h"])
            draw.rectangle((x, y, x + w, y + h), fill="black")
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping invalid mask_region %r: %s", region, exc)
    return image


def _is_screen_locked(image: "Any") -> bool:
    """Return True if the screen appears to be locked (very dark).

    Uses mean luminance of the grayscale image. A value below 5 suggests
    a blank/locked screen — skip saving to avoid storing useless frames.
    """
    from PIL import ImageStat  # type: ignore[import-untyped]
    mean = ImageStat.Stat(image.convert("L")).mean[0]
    return mean < 5


def _downsize(image: "Any", max_dim: int) -> "Any":
    """Downsize image so longest side <= max_dim, preserving aspect ratio."""
    from PIL import Image  # type: ignore[import-untyped]
    image.thumbnail((max_dim, max_dim), Image.LANCZOS)
    return image


def _save_frame(
    image: "Any",
    local_dir: Path,
    run_name: str | None,
    quality: int,
    max_dimension: int,
    *,
    mask_regions: list[dict] | None = None,
    window_title: str | None = None,
) -> Path | None:
    """Apply masks, downsize, stamp EXIF, and write the JPEG.

    Returns the saved path, or None on failure.
    """
    if mask_regions:
        image = _apply_masks(image, mask_regions)

    image = _downsize(image, max_dimension)

    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M%S")

    def _safe(s: str) -> str:
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)

    title_suffix = f"_{_safe(window_title)}" if window_title else ""
    if run_name is not None:
        safe_run = _safe(run_name)
        filename = f"{time_str}{_RUNEND_MARKER}{safe_run}{title_suffix}.jpg"
    else:
        filename = f"{time_str}{title_suffix}.jpg"

    dest_dir = local_dir / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    # Build EXIF UserComment: login + window title.
    exif_bytes: bytes | None = None
    try:
        import piexif  # type: ignore[import-untyped]
        try:
            login = os.getlogin()
        except Exception:
            login = "unknown"
        caption = f"user={login} window={window_title or 'fullscreen'}"
        user_comment = b"ASCII\x00\x00\x00" + caption.encode("ascii", errors="replace")
        exif_dict: dict = {
            "Exif": {piexif.ExifIFD.UserComment: user_comment},
        }
        exif_bytes = piexif.dump(exif_dict)
    except ImportError:
        pass  # piexif is optional; EXIF is best-effort
    except Exception as exc:
        logger.debug("EXIF build failed: %s", exc)

    try:
        save_kwargs: dict[str, Any] = {"format": "JPEG", "quality": quality, "optimize": True}
        if exif_bytes is not None:
            save_kwargs["exif"] = exif_bytes
        # piexif may not be available; PIL accepts exif= kwarg directly only when
        # it's valid bytes, so we guard here.
        try:
            image.save(str(dest), **save_kwargs)
        except (TypeError, AttributeError):
            # Fallback: save without exif
            save_kwargs.pop("exif", None)
            image.save(str(dest), **save_kwargs)
        logger.debug("Screencap saved: %s", dest)
        return dest
    except Exception as exc:
        logger.warning("Failed to save screencap to %s: %s", dest, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def capture_now(
    config: ScreencapConfig,
    *,
    run_name: str | None = None,
) -> Path | None:
    """Capture one frame to local_dir.

    Returns path of saved JPEG, or None if config.enabled is False,
    screen was locked (luminance < 5), or capture failed.

    If run_name is set the frame is treated as an acquisition-end frame —
    saved with run_name in the filename and exempted from mirror retention.

    Tries window-by-title (each title in config.window_titles); falls back
    to full screen if config.fallback_full_screen and no window matches.

    Args:
        config: ScreencapConfig to use.
        run_name: If provided, marks this as a run-end frame.

    Returns:
        Path to saved JPEG, or None.
    """
    if not config.enabled:
        logger.debug("screencap disabled — skipping capture_now")
        return None

    def _save(shot, title: str | None) -> Path | None:
        try:
            image = _mss_to_pil(shot)
        except Exception as exc:
            logger.warning("mss → PIL conversion failed: %s", exc)
            return None
        if _is_screen_locked(image):
            logger.debug("Screen appears locked (luminance < 5) — skipping save")
            return None
        return _save_frame(
            image,
            config.local_dir,
            run_name,
            config.quality,
            config.max_dimension,
            mask_regions=list(config.mask_regions),
            window_title=title,
        )

    saved_paths: list[Path] = []

    if config.capture_all_windows and config.window_titles:
        # Capture every matching window as a separate frame.
        for title in config.window_titles:
            shot = _grab_window(title)
            if shot is None:
                logger.debug("No window matched %r — skipping", title)
                continue
            p = _save(shot, title)
            if p is not None:
                saved_paths.append(p)
        # If nothing matched at all, optionally fall back to full screen.
        if not saved_paths and config.fallback_full_screen:
            shot = _grab_fullscreen()
            if shot is not None:
                p = _save(shot, None)
                if p is not None:
                    saved_paths.append(p)
    else:
        # Original behaviour: first matching window, fall back to full screen.
        shot = None
        captured_title: str | None = None
        for title in config.window_titles:
            shot = _grab_window(title)
            if shot is not None:
                captured_title = title
                logger.debug("Captured window: %r", title)
                break
        if shot is None and config.fallback_full_screen:
            shot = _grab_fullscreen()
            logger.debug("Captured full screen")
        if shot is None:
            logger.warning("No screen capture source available")
            return None
        p = _save(shot, captured_title)
        if p is not None:
            saved_paths.append(p)

    if not saved_paths:
        return None
    if len(saved_paths) > 1:
        logger.info("capture_now: saved %d frames", len(saved_paths))
    return saved_paths[0]


def run_daemon(config: ScreencapConfig, *, stop_event: threading.Event | None = None) -> None:
    """Heartbeat loop — captures every config.heartbeat_min minutes.

    Exits when stop_event is set (or the process is killed). Intended to
    run in the foreground; a supervisor (start_stan_loop.bat or similar)
    is responsible for relaunch.

    Args:
        config: ScreencapConfig to use.
        stop_event: Optional threading.Event; daemon exits when set.
    """
    if stop_event is None:
        stop_event = threading.Event()

    interval_sec = max(1, config.heartbeat_min) * 60
    logger.info(
        "screencap-daemon started — heartbeat every %d min", config.heartbeat_min
    )

    while not stop_event.is_set():
        path = capture_now(config)
        if path:
            logger.info("Screencap: %s", path)
        else:
            logger.debug("Screencap skipped (disabled or locked screen)")

        # Sleep in small increments so stop_event is checked promptly.
        elapsed = 0.0
        while elapsed < interval_sec and not stop_event.is_set():
            sleep_chunk = min(5.0, interval_sec - elapsed)
            stop_event.wait(sleep_chunk)
            elapsed += sleep_chunk

    logger.info("screencap-daemon stopped")


def on_acquisition_end(raw_path: Path, config: ScreencapConfig) -> Path | None:
    """Called when a raw file becomes stable.

    Captures one frame indexed by run_name (raw_path.stem). Returns
    path or None.

    Args:
        raw_path: Path to the raw file that became stable.
        config: ScreencapConfig to use.

    Returns:
        Path to saved JPEG, or None.
    """
    if not config.on_acquisition_end:
        return None
    run_name = raw_path.stem
    logger.debug("on_acquisition_end: capturing for run %r", run_name)
    return capture_now(config, run_name=run_name)


def prune_screencaps(
    local_dir: Path,
    mirror_dir: Path | None,
    config: ScreencapConfig,
) -> dict:
    """Prune old heartbeat screencaps; keep run-end frames forever in mirror.

    Deletes:
    - Local heartbeat frames older than config.local_retention_days.
    - Mirror heartbeat frames older than config.mirror_retention_hours.

    Retains:
    - All frames whose filename contains _RUNEND_MARKER in mirror_dir.

    Args:
        local_dir: Local screencaps directory.
        mirror_dir: Mirror screencaps directory, or None.
        config: ScreencapConfig for retention policy.

    Returns:
        dict with keys local_deleted, mirror_deleted, mirror_retained_runend.
    """
    now = datetime.now()
    local_cutoff = now - timedelta(days=config.local_retention_days)
    mirror_cutoff = now - timedelta(hours=config.mirror_retention_hours)

    local_deleted = 0
    mirror_deleted = 0
    mirror_retained_runend = 0

    # Prune local dir.
    if local_dir.exists():
        for jpg in local_dir.rglob("*.jpg"):
            if _RUNEND_MARKER in jpg.name:
                # Run-end frames are kept locally too (they are "one per run").
                continue
            try:
                mtime = datetime.fromtimestamp(jpg.stat().st_mtime)
                if mtime < local_cutoff:
                    jpg.unlink()
                    local_deleted += 1
            except Exception as exc:
                logger.debug("Could not prune local %s: %s", jpg, exc)

    # Prune mirror dir.
    if mirror_dir is not None and mirror_dir.exists():
        for jpg in mirror_dir.rglob("*.jpg"):
            if _RUNEND_MARKER in jpg.name:
                mirror_retained_runend += 1
                continue  # run-end frames are retained indefinitely
            try:
                mtime = datetime.fromtimestamp(jpg.stat().st_mtime)
                if mtime < mirror_cutoff:
                    jpg.unlink()
                    mirror_deleted += 1
            except Exception as exc:
                logger.debug("Could not prune mirror %s: %s", jpg, exc)

    return {
        "local_deleted": local_deleted,
        "mirror_deleted": mirror_deleted,
        "mirror_retained_runend": mirror_retained_runend,
    }
