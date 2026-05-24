#!/usr/bin/env python3
"""Generate the integration's icon assets from the canonical Python
spec below. Outputs:

  assets/icon.png          — 256×256, transparent, for HACS branding
  assets/icon@2x.png       — 512×512, transparent, for HACS @2x
  assets/social-preview.png — 1280×640, dark backdrop, for GitHub OG

Run from the repo root:  python3 scripts/build-icons.py

Why Python and not the SVG via qlmanage: qlmanage rasterizes against
a white background and doesn't honor SVG aspect ratios cleanly. PIL
gives precise transparency + sizing control and re-runs deterministically.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).parent.parent / "assets"

# ── Color palette ──────────────────────────────────────────────────
# Matches assets/icon.svg gradient stops. If you change these, also
# update the SVG so the source-of-truth and the rendered PNG agree.
TOP_LIGHT = (127, 205, 240, 255)  # #7fcdf0
MID = (41, 182, 246, 255)  # #29b6f6
DEEP = (2, 119, 189, 255)  # #0277bd

# Social preview backdrop (gradient stops)
BG_TOP = (15, 24, 32)
BG_BOTTOM = (28, 38, 48)
TEXT_PRIMARY = (255, 255, 255)
TEXT_SECONDARY = (176, 196, 212)
TEXT_ACCENT = (127, 205, 240)


def _draw_drop(size: int) -> Image.Image:
    """Render a classic water-drop at the given square pixel size with
    a transparent background. The drop fills ~85% of the canvas with
    even margin on all sides."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # The drop silhouette mirrors the SVG path's outline:
    # point at top, rounded base. Built via a triangle (top half) +
    # circle (bottom half), then filtered to merge into one teardrop.
    # Coordinates in 256-unit space then scaled.
    s = size / 256.0
    # Bottom circle: center (128, 162), radius 72
    cx, cy, r = 128 * s, 162 * s, 72 * s
    # Top apex
    apex = (128 * s, 24 * s)
    # Side tangent points (rough; the path looks smooth at this scale)
    # We'll use a polygon for the upper teardrop wedge and a circle
    # for the rounded base. PIL antialiases by upscaling-then-downscaling.

    # Upscale 4x for smoother edges, then downscale at the end.
    scale = 4
    up = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    udraw = ImageDraw.Draw(up)

    def ux(v):
        return v * scale

    # Path approximation: a polygon for the top wedge + a filled ellipse
    # for the bottom rounded part. Together they form the teardrop.
    polygon = [
        (ux(apex[0]), ux(apex[1])),
        (ux((cx - r) + r * 0.18), ux(cy - r * 0.82)),
        (ux(cx - r), ux(cy)),
        (ux(cx + r), ux(cy)),
        (ux((cx + r) - r * 0.18), ux(cy - r * 0.82)),
    ]
    udraw.polygon(polygon, fill=MID)
    udraw.ellipse(
        [
            ux(cx - r),
            ux(cy - r),
            ux(cx + r),
            ux(cy + r),
        ],
        fill=MID,
    )

    # Down-sample for smooth edges
    img = up.resize((size, size), Image.LANCZOS)

    # Apply a vertical gradient over the drop's silhouette so it
    # reads as 3D. Build a separate gradient image and mask it by
    # the drop alpha.
    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for y in range(size):
        # 0 at top, 1 at bottom
        t = y / (size - 1)
        if t < 0.55:
            # Blend TOP_LIGHT → MID
            tt = t / 0.55
            color = _lerp(TOP_LIGHT, MID, tt)
        else:
            # Blend MID → DEEP
            tt = (t - 0.55) / 0.45
            color = _lerp(MID, DEEP, tt)
        gdraw.line([(0, y), (size, y)], fill=color)
    # Mask gradient by drop alpha
    drop_alpha = img.split()[3]
    grad.putalpha(drop_alpha)

    # Add the inner shine: an offset elliptical highlight, soft-blurred
    shine = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shine)
    sdraw.ellipse(
        [
            int(81 * s),
            int(106 * s),
            int(127 * s),
            int(170 * s),
        ],
        fill=(255, 255, 255, 130),
    )
    shine = shine.filter(ImageFilter.GaussianBlur(radius=size * 0.04))
    # Mask the shine by the drop silhouette so it doesn't bleed outside
    shine_alpha = Image.new("L", (size, size), 0)
    shine_alpha.paste(shine.split()[3], (0, 0), drop_alpha)
    shine.putalpha(shine_alpha)
    grad = Image.alpha_composite(grad, shine)
    return grad


def _lerp(a, b, t):
    """Linear-interpolate two RGBA tuples."""
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(4))


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Best-available system font for the social-preview text."""
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _build_social_preview(icon: Image.Image) -> Image.Image:
    """1280×640 dark-backdrop banner with the icon + title text."""
    width, height = 1280, 640
    canvas = Image.new("RGBA", (width, height), (*BG_TOP, 255))
    # Vertical background gradient
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        t = y / (height - 1)
        color = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=(*color, 255))

    # Paste icon scaled to 440px, vertically centered, left-padded
    icon_size = 440
    icon_scaled = icon.resize((icon_size, icon_size), Image.LANCZOS)
    canvas.paste(icon_scaled, (100, (height - icon_size) // 2), icon_scaled)

    # Title text on the right. Auto-size title down if it would overflow.
    title_text = "Complete Irrigation"
    title_size = 80
    title_font = _load_font(title_size)
    text_left = 600
    max_width = width - text_left - 60
    while title_font.getlength(title_text) > max_width and title_size > 40:
        title_size -= 2
        title_font = _load_font(title_size)

    sub_font = _load_font(34)
    tag_font = _load_font(22)

    draw.text((text_left, 232), title_text, fill=(*TEXT_PRIMARY, 255), font=title_font)
    draw.text(
        (text_left, 322),
        "Home Assistant custom integration",
        fill=(*TEXT_SECONDARY, 255),
        font=sub_font,
    )
    draw.text(
        (text_left, 400),
        "Schedules  ·  Run history  ·  Weather gates  ·  Moisture-aware",
        fill=(*TEXT_ACCENT, 255),
        font=tag_font,
    )
    return canvas.convert("RGB")  # PNG with no alpha for social preview


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon_512 = _draw_drop(512)
    icon_512.save(ASSETS / "icon@2x.png", "PNG", optimize=True)
    icon_256 = icon_512.resize((256, 256), Image.LANCZOS)
    icon_256.save(ASSETS / "icon.png", "PNG", optimize=True)
    social = _build_social_preview(icon_512)
    social.save(ASSETS / "social-preview.png", "PNG", optimize=True)
    print(f"wrote {ASSETS / 'icon.png'} (256×256)")
    print(f"wrote {ASSETS / 'icon@2x.png'} (512×512)")
    print(f"wrote {ASSETS / 'social-preview.png'} (1280×640)")


if __name__ == "__main__":
    main()
