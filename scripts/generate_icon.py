#!/usr/bin/env python3
"""Generate the JARVIS desktop icon (jarvis.png + jarvis.ico) at the repo root.

Draws the HUD orb — a glowing cyan core with an orbital ring on a dark disc —
so desktop shortcuts get a proper brand icon without committing binaries.
Requires Pillow; exits quietly with a hint when it is missing so launchers
can call this unconditionally.
"""

import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("Pillow not installed — skipping icon generation (pip install pillow).")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
SIZE = 256
CENTER = SIZE / 2

BG = (10, 14, 20)          # HUD background #0a0e14
CYAN = (0, 212, 255)       # accent #00d4ff


def _radial_layer(cx: float, cy: float, radius: float, color, max_alpha: int) -> Image.Image:
    """Soft radial glow as a separate RGBA layer. The alpha mask is computed
    per-pixel at low resolution and bicubic-upscaled, giving a smooth gradient
    without the banding/saturation artifacts of stacked translucent circles."""
    small = 96
    scale = SIZE / small
    mask = Image.new("L", (small, small), 0)
    px = mask.load()
    for y in range(small):
        for x in range(small):
            d = math.hypot((x + 0.5) * scale - cx, (y + 0.5) * scale - cy)
            if d < radius:
                px[x, y] = int(max_alpha * (1 - d / radius) ** 2)
    mask = mask.resize((SIZE, SIZE), Image.BICUBIC)
    layer = Image.new("RGBA", (SIZE, SIZE), (*color, 255))
    layer.putalpha(mask)
    return layer


def build_icon() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Dark disc base with a subtle cyan rim.
    draw.ellipse((4, 4, SIZE - 4, SIZE - 4), fill=(*BG, 255))
    draw.ellipse((4, 4, SIZE - 4, SIZE - 4), outline=(*CYAN, 90), width=3)

    # Core glow + bright center.
    img.alpha_composite(_radial_layer(CENTER, CENTER, 100, CYAN, 80))
    img.alpha_composite(_radial_layer(CENTER, CENTER, 58, CYAN, 150))
    img.alpha_composite(_radial_layer(CENTER, CENTER, 32, (110, 228, 255), 210))
    img.alpha_composite(_radial_layer(CENTER, CENTER, 18, (200, 246, 255), 250))
    draw = ImageDraw.Draw(img)
    draw.ellipse((CENTER - 9, CENTER - 9, CENTER + 9, CENTER + 9), fill=(232, 252, 255, 255))

    # Orbital ring (ellipse rotated by drawing on a separate layer).
    ring = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(ring)
    ring_draw.ellipse((CENTER - 96, CENTER - 38, CENTER + 96, CENTER + 38), outline=(*CYAN, 200), width=5)
    ring = ring.rotate(-24, resample=Image.BICUBIC, center=(CENTER, CENTER))
    img.alpha_composite(ring)

    # Orbiting particles along the ring.
    for angle_deg, r_dot in ((30, 7), (160, 5), (255, 6)):
        theta = math.radians(angle_deg)
        ex, ey = 96 * math.cos(theta), 38 * math.sin(theta)
        rot = math.radians(-24)
        px = CENTER + ex * math.cos(rot) - ey * math.sin(rot)
        py = CENTER + ex * math.sin(rot) + ey * math.cos(rot)
        img.alpha_composite(_radial_layer(px, py, r_dot * 2.6, CYAN, 160))
        draw = ImageDraw.Draw(img)
        draw.ellipse((px - r_dot, py - r_dot, px + r_dot, py + r_dot), fill=(210, 248, 255, 255))

    return img


def main() -> None:
    icon = build_icon()
    png_path = ROOT / "jarvis.png"
    ico_path = ROOT / "jarvis.ico"
    icon.save(png_path)
    icon.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Wrote {png_path}")
    print(f"Wrote {ico_path}")


if __name__ == "__main__":
    main()
