"""Generate STAN PWA icons (iOS apple-touch + Android maskable + PWA standard).

Run from repo root: python3 scripts/generate_pwa_icons.py
Writes to: stan/dashboard/public/icons/

Design: gold 'STAN' wordmark (UC Davis #DAAA00) on a navy radial gradient
(#011a3a -> #022851), matching the dashboard header. A small chromatographic
peak sits beneath the wordmark as a subtle proteomics nod.

Sizes generated:
  - icon-192.png         PWA standard small
  - icon-512.png         PWA standard large
  - icon-512-maskable.png Android adaptive (10% safe-area padding)
  - apple-touch-icon.png  iOS Home Screen (180x180)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


NAVY_DEEP = (1, 26, 58)
NAVY_MID = (3, 48, 95)
GOLD = (218, 170, 0)
GOLD_BRIGHT = (255, 211, 64)

OUT = Path(__file__).resolve().parent.parent / "stan" / "dashboard" / "public" / "icons"


def _radial_gradient(size: int, inner: tuple, outer: tuple) -> Image.Image:
    img = Image.new("RGB", (size, size), outer)
    cx = size * 0.5
    cy = size * 0.45
    max_r = math.hypot(size, size)
    px = img.load()
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - cx, y - cy) / max_r
            t = max(0.0, min(1.0, d * 1.6))
            r = int(inner[0] + (outer[0] - inner[0]) * t)
            g = int(inner[1] + (outer[1] - inner[1]) * t)
            b = int(inner[2] + (outer[2] - inner[2]) * t)
            px[x, y] = (r, g, b)
    return img


def _font(size_px: int) -> ImageFont.FreeTypeFont:
    """Best available bold sans-serif with broad weight."""
    candidates = [
        ("/System/Library/Fonts/HelveticaNeue.ttc", 1),     # Helvetica Neue Bold
        ("/System/Library/Fonts/HelveticaNeue.ttc", 7),     # Helvetica Neue Black
        ("/System/Library/Fonts/Helvetica.ttc", 1),
        ("/System/Library/Fonts/Avenir Next.ttc", 4),       # Avenir Next Bold
        ("/Library/Fonts/Arial Bold.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ]
    for path, idx in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size_px, index=idx)
            except (OSError, IOError):
                try:
                    return ImageFont.truetype(path, size_px)
                except (OSError, IOError):
                    continue
    return ImageFont.load_default()


def _peak_under_text(size: int, baseline_y: int, peak_color: tuple) -> Image.Image:
    """A compact gold peak silhouette positioned under the wordmark."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    apex_x = size * 0.5
    apex_y = baseline_y - int(size * 0.18)
    width_l = size * 0.08
    width_r = size * 0.10  # right-skewed peak

    pts = []
    span_left = int(size * 0.20)
    span_right = int(size * 0.80)
    for x in range(span_left, span_right + 1):
        sigma = width_l if x <= apex_x else width_r
        y_off = math.exp(-((x - apex_x) ** 2) / (2 * sigma * sigma))
        h = baseline_y - apex_y
        y = baseline_y - h * y_off
        pts.append((x, y))
    poly = [(span_left, baseline_y)] + pts + [(span_right, baseline_y)]
    draw.polygon(poly, fill=peak_color)

    # Crisp peak outline.
    outline_w = max(2, size // 180)
    draw.line([(int(p[0]), int(p[1])) for p in pts],
              fill=GOLD_BRIGHT + (255,), width=outline_w)

    # Apex highlight dot.
    apex_radius = max(2, size // 110)
    draw.ellipse(
        (apex_x - apex_radius, apex_y - apex_radius,
         apex_x + apex_radius, apex_y + apex_radius),
        fill=(255, 255, 220, 240),
    )

    # Baseline tick.
    bl_w = max(2, size // 200)
    draw.line(
        [(span_left, baseline_y), (span_right, baseline_y)],
        fill=GOLD + (255,), width=bl_w,
    )
    return layer


def render_master(size: int, safe_area_pct: float = 0.0) -> Image.Image:
    bg = _radial_gradient(size, NAVY_MID, NAVY_DEEP).convert("RGBA")

    fg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fg)

    # ---- Wordmark "STAN" -----------------------------------------------
    text = "STAN"
    # Find a font size where the wordmark fills ~70% of the canvas width.
    target_w = int(size * 0.72)
    font_px = int(size * 0.48)
    font = _font(font_px)
    for _ in range(10):
        bbox = fd.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        if abs(tw - target_w) < size * 0.02 or tw == 0:
            break
        scale = target_w / max(1, tw)
        font_px = int(font_px * scale)
        font = _font(font_px)
    bbox = fd.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = (size * 0.36) - th / 2 - bbox[1]

    # Soft shadow under the text for separation from the navy bg.
    shadow_offset = max(2, size // 200)
    fd.text((tx + shadow_offset, ty + shadow_offset), text,
            fill=(0, 0, 0, 130), font=font)
    fd.text((tx, ty), text, fill=GOLD + (255,), font=font)

    # ---- Peak underneath the wordmark ----------------------------------
    baseline_y = int(size * 0.78)
    peak = _peak_under_text(size, baseline_y,
                            peak_color=(GOLD[0], GOLD[1], GOLD[2], 200))
    fg.alpha_composite(peak)

    # ---- Optional safe-area shrink for Android maskable ----------------
    if safe_area_pct > 0:
        scale = 1 - 2 * safe_area_pct
        new_size = int(size * scale)
        fg_small = fg.resize((new_size, new_size), Image.LANCZOS)
        offset = (size - new_size) // 2
        fg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        fg.alpha_composite(fg_small, (offset, offset))

    bg.alpha_composite(fg)
    return bg


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    master = render_master(1024)
    master.resize((512, 512), Image.LANCZOS).convert("RGB").save(
        OUT / "icon-512.png", optimize=True
    )
    master.resize((192, 192), Image.LANCZOS).convert("RGB").save(
        OUT / "icon-192.png", optimize=True
    )
    master.resize((180, 180), Image.LANCZOS).convert("RGB").save(
        OUT / "apple-touch-icon.png", optimize=True
    )

    maskable = render_master(1024, safe_area_pct=0.10)
    maskable.resize((512, 512), Image.LANCZOS).convert("RGB").save(
        OUT / "icon-512-maskable.png", optimize=True
    )

    print(f"Wrote 4 icons to {OUT}")
    for p in sorted(OUT.glob("*.png")):
        print(f"  {p.name}  ({p.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
